from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest

from schemabridge.adapters.query_studio.atomic_preflight import (
    BoundaryScreenedDescriptionExpansionPreflight,
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
from schemabridge.domain.concepts import (
    CanonicalType,
    LogicalFieldRef,
    LogicalModelRef,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    DescriptionExpansionRoute,
    DescriptionQuery,
    DescriptionSearchProbe,
    OpaqueCandidateId,
    ProposedMetric,
    ProviderConfigurationFacts,
    ProviderOutcomeCode,
    ProviderStage,
    ProviderUsageFacts,
    QueryFieldPurpose,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    QueryStudioModelProposal,
    QueryStudioPromptCandidate,
    QueryStudioPromptModel,
    QueryStudioPromptVocabulary,
    QueryStudioRequiredSelectionCounts,
    SemanticMatchState,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import (
    DateGrain,
    FilterOperator,
    MetricOperation,
    SortDirection,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
RESERVATION_ID = "air_" + "c" * 64
AUDIT_ID = "aia_" + "d" * 64


@dataclass
class _Nonces:
    index: int = 0

    def new_nonce(self) -> str:
        self.index += 1
        return f"capability_{self.index:04d}_" + "x" * 32


@dataclass
class _Control:
    configuration: ProviderConfigurationFacts
    outcomes: list[AiAdmissionOutcome] = field(
        default_factory=lambda: [AiAdmissionOutcome.RESERVED]
    )
    remaining_input_tokens: int | None = None
    reservations: list[AiAttemptReservationRequest] = field(default_factory=list)
    settlements: list[AiAttemptSettlementRequest] = field(default_factory=list)

    def reserve(self, request: AiAttemptReservationRequest) -> AiAttemptReservation:
        self.reservations.append(request)
        outcome = (
            AiAdmissionOutcome.QUOTA_EXHAUSTED
            if self.remaining_input_tokens is not None
            and request.estimated_input_tokens > self.remaining_input_tokens
            else self.outcomes.pop(0)
        )
        admitted = outcome in {
            AiAdmissionOutcome.RESERVED,
            AiAdmissionOutcome.REPLAYED,
        }
        return AiAttemptReservation(
            outcome=outcome,
            replayed=outcome is AiAdmissionOutcome.REPLAYED,
            reservation_id=RESERVATION_ID if admitted else None,
            status=AiAttemptStatus.RESERVED if admitted else None,
            fencing_token=len(self.reservations) if admitted else None,
            lease_expires_at=NOW if admitted else None,
            policy_version=1 if admitted else 0,
            model_snapshot=(self.configuration.model_snapshot if admitted else None),
            endpoint_region=(self.configuration.endpoint_region if admitted else None),
            configuration_fingerprint=(self.configuration.fingerprint if admitted else None),
        )

    def settle(self, request: AiAttemptSettlementRequest) -> AiAttemptSettlement:
        self.settlements.append(request)
        reservation = self.reservations[-1]
        return AiAttemptSettlement(
            reservation_id=request.reservation_id,
            replayed=False,
            status=AiAttemptStatus.SETTLED,
            outcome=request.outcome,
            charged_input_tokens=(
                request.observed_input_tokens
                if request.outcome is AiSettlementOutcome.SUCCEEDED
                else reservation.estimated_input_tokens
            ),
            charged_output_tokens=(
                request.observed_output_tokens
                if request.outcome is AiSettlementOutcome.SUCCEEDED
                else reservation.estimated_output_tokens
            ),
            settled_at=NOW,
            audit_id=AUDIT_ID,
        )

    def expire(self, workspace_id: str, *, limit: int = 100) -> int:
        del workspace_id, limit
        return 0


@dataclass
class _DiscordantSettlementControl(_Control):
    settlement_update: dict[str, object] = field(default_factory=dict)

    def settle(self, request: AiAttemptSettlementRequest) -> AiAttemptSettlement:
        return replace(super().settle(request), **self.settlement_update)


@dataclass
class _Expansion:
    configuration: ProviderConfigurationFacts
    failures: list[QueryStudioPortError] = field(default_factory=list)
    calls: int = 0

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        del value
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return DescriptionExpansionResult(
            expansion=DescriptionExpansion(
                probes=(
                    DescriptionSearchProbe(
                        purpose_id="customer_metric",
                        query=DescriptionQuery("customer identifier"),
                        intended_use=QueryFieldPurpose.METRIC,
                        metric_operation=MetricOperation.COUNT_DISTINCT,
                    ),
                )
            ),
            usage=_usage(self.configuration, ProviderStage.EXPANSION),
        )


@dataclass
class _ExpansionWithUsage:
    delegate: _Expansion
    usage: ProviderUsageFacts
    calls: int = 0

    @property
    def configuration(self) -> ProviderConfigurationFacts:
        return self.delegate.configuration

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        self.calls += 1
        result = self.delegate.expand(value)
        return result.model_copy(update={"usage": self.usage})


@dataclass
class _Intent:
    configuration: ProviderConfigurationFacts
    calls: int = 0

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        self.calls += 1
        candidate = value.vocabulary.candidates[0].candidate_id
        return QueryStudioInterpretationResult(
            proposal=QueryStudioModelProposal(
                semantic_state=SemanticMatchState.ALIGNED,
                primary_candidate_id=candidate,
                metrics=(
                    ProposedMetric(
                        candidate_id=candidate,
                        operation=MetricOperation.COUNT_DISTINCT,
                    ),
                ),
            ),
            usage=_usage(self.configuration, ProviderStage.INTERPRETATION),
        )


@dataclass
class _IntentWithUsage:
    delegate: _Intent
    usage: ProviderUsageFacts
    calls: int = 0

    @property
    def configuration(self) -> ProviderConfigurationFacts:
        return self.delegate.configuration

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        self.calls += 1
        result = self.delegate.interpret(value)
        return result.model_copy(update={"usage": self.usage})


def _configuration() -> ProviderConfigurationFacts:
    return ProviderConfigurationFacts.create(
        adapter="openai_responses_structured",
        model_snapshot="gpt-5-nano-2025-08-07",
        reasoning_effort="minimal",
        endpoint_region="eu",
        prompt_version="m27-v1",
        schema_version="m27-v1",
        matcher_version="m27-v1",
        attempt_policy_version="m27-durable-attempts-v3",
        external_ai=True,
    )


def _usage(
    configuration: ProviderConfigurationFacts,
    stage: ProviderStage,
) -> ProviderUsageFacts:
    return ProviderUsageFacts(
        stage=stage,
        model_snapshot=configuration.model_snapshot,
        configuration_fingerprint=configuration.fingerprint,
        input_tokens=8,
        output_tokens=3,
        duration_ms=17,
        outcome=ProviderOutcomeCode.SUCCEEDED,
    )


def _expansion_wrapper(
    delegate: _Expansion,
    control: _Control,
    *,
    local: bool = False,
) -> AdmittedDescriptionExpansion:
    @dataclass(frozen=True)
    class _AlwaysExternalPreflight:
        def expand_if_local(
            self,
            _value: DescriptionExpansionInput,
        ) -> DescriptionExpansion | None:
            return None

    return AdmittedDescriptionExpansion(
        delegate=delegate,
        preflight=(
            BoundaryScreenedDescriptionExpansionPreflight() if local else _AlwaysExternalPreflight()
        ),
        control=control,
        nonces=_Nonces(),
        workspace_id="workspace-alpha",
        actor_digest=SHA_A,
        semantic_scope_fingerprint=SHA_B,
        configuration=delegate.configuration,
        estimated_input_tokens=48_000,
        estimated_output_tokens=4_096,
    )


def _input() -> DescriptionExpansionInput:
    return DescriptionExpansionInput(
        text=DescriptionQuery("count active customers"),
        language=UserLanguage.ENGLISH,
        lane=DescriptionExpansionRoute.ANALYTICAL,
    )


def _vocabulary() -> QueryStudioPromptVocabulary:
    candidate = OpaqueCandidateId("qsc1_" + "e" * 64)
    return QueryStudioPromptVocabulary(
        context_source="recorded:synthetic",
        context_version=1,
        models=(
            QueryStudioPromptModel(
                id=LogicalModelRef("Customer"),
                definition="Synthetic customer model.",
            ),
        ),
        candidates=(
            QueryStudioPromptCandidate(
                candidate_id=candidate,
                logical_field=LogicalFieldRef("Customer.customer_key"),
                definition="Stable synthetic customer identifier.",
                canonical_type=CanonicalType.STRING,
                role=LogicalFieldRole.IDENTIFIER,
                intended_uses=(QueryFieldPurpose.METRIC,),
                score=10_000,
            ),
        ),
        required_selection_counts=QueryStudioRequiredSelectionCounts(
            dimensions=0,
            metrics=1,
            filters=0,
        ),
        metric_operations=tuple(MetricOperation),
        filter_operators=tuple(FilterOperator),
        date_grains=tuple(DateGrain),
        sort_directions=tuple(SortDirection),
    )


def _interpretation_expansion() -> DescriptionExpansion:
    return DescriptionExpansion(
        probes=(
            DescriptionSearchProbe(
                purpose_id="customer_metric",
                query=DescriptionQuery("customer identifier"),
                source_span="customers",
                intended_use=QueryFieldPurpose.METRIC,
                metric_operation=MetricOperation.COUNT_DISTINCT,
            ),
        )
    )


def test_successful_expansion_is_reserved_and_settled_with_observed_usage() -> None:
    configuration = _configuration()
    delegate = _Expansion(configuration)
    control = _Control(configuration)

    result = _expansion_wrapper(delegate, control).expand(_input())

    assert result.usage.input_tokens == 8
    assert delegate.calls == 1
    assert [item.attempt_number for item in control.reservations] == [1]
    assert control.reservations[0].estimated_input_tokens == 48_000
    assert control.reservations[0].estimated_output_tokens == 4_096
    assert control.settlements[0].outcome is AiSettlementOutcome.SUCCEEDED
    assert control.settlements[0].observed_input_tokens == 8
    assert control.settlements[0].observed_output_tokens == 3
    assert "count active customers" not in repr(control.reservations[0])
    assert "capability_" not in repr(control.reservations[0])


def test_expansion_admission_accepts_the_complete_4096_output_token_budget() -> None:
    configuration = _configuration()
    delegate = _ExpansionWithUsage(
        delegate=_Expansion(configuration),
        usage=_usage(configuration, ProviderStage.EXPANSION).model_copy(
            update={"output_tokens": 4_096}
        ),
    )
    control = _Control(configuration)

    result = _expansion_wrapper(delegate, control).expand(_input())

    assert result.usage.output_tokens == 4_096
    assert control.reservations[0].estimated_output_tokens == 4_096
    assert control.settlements[0].observed_output_tokens == 4_096
    assert control.settlements[0].outcome is AiSettlementOutcome.SUCCEEDED


@pytest.mark.parametrize(
    "settlement_update",
    (
        {"reservation_id": "air_" + "f" * 64},
        {"replayed": True},
        {"status": AiAttemptStatus.EXPIRED},
        {"outcome": AiSettlementOutcome.INVALID_OUTPUT},
        {"charged_input_tokens": 7},
        {"charged_output_tokens": 2},
        {"settled_at": datetime(2026, 7, 26, 12, 0)},
        {"audit_id": "aia_" + "F" * 64},
    ),
)
def test_success_requires_an_exact_settlement_acknowledgement(
    settlement_update: dict[str, object],
) -> None:
    configuration = _configuration()
    delegate = _Expansion(configuration)
    control = _DiscordantSettlementControl(
        configuration,
        settlement_update=settlement_update,
    )

    with pytest.raises(QueryStudioPortError) as captured:
        _expansion_wrapper(delegate, control).expand(_input())

    assert captured.value.code is QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE
    assert delegate.calls == 1
    assert len(control.reservations) == len(control.settlements) == 1


def test_field_match_lane_is_local_before_admission_and_preserves_the_full_description() -> None:
    configuration = _configuration()
    delegate = _Expansion(configuration)
    control = _Control(configuration)
    description = "fecha de registro del cliente"

    result = _expansion_wrapper(delegate, control, local=True).expand(
        DescriptionExpansionInput(
            text=DescriptionQuery(description),
            language=UserLanguage.SPANISH,
            lane=DescriptionExpansionRoute.FIELD_MATCH,
        )
    )

    assert result.usage is None
    assert len(result.expansion.probes) == 1
    probe = result.expansion.probes[0]
    assert probe.purpose_id == "atomic_field"
    assert probe.query.root == probe.source_span == description
    assert probe.intended_use is QueryFieldPurpose.DIMENSION
    assert probe.roles == ()
    assert probe.canonical_types == ()
    assert probe.metric_operation is None
    assert probe.filter_operator is None
    assert probe.date_grain is None
    assert delegate.calls == 0
    assert control.reservations == []
    assert control.settlements == []


def test_field_match_lane_runs_the_production_privacy_screen_before_local_routing() -> None:
    configuration = _configuration()
    delegate = _Expansion(configuration)
    control = _Control(configuration)
    sensitive = "usa " + "sk-" + "proj-AbCdEfGhIjKlMnOpQrStUv"

    with pytest.raises(QueryStudioPortError) as captured:
        _expansion_wrapper(delegate, control, local=True).expand(
            DescriptionExpansionInput(
                text=DescriptionQuery(sensitive),
                language=UserLanguage.SPANISH,
                lane=DescriptionExpansionRoute.FIELD_MATCH,
            )
        )

    assert captured.value.code is QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED
    assert delegate.calls == 0
    assert control.reservations == []
    assert control.settlements == []


def test_analytical_expansion_is_local_before_admission_and_uses_no_provider() -> None:
    configuration = _configuration()
    delegate = _Expansion(configuration)
    control = _Control(configuration)

    result = _expansion_wrapper(delegate, control, local=True).expand(
        DescriptionExpansionInput(
            text=DescriptionQuery("customer_id"),
            language=UserLanguage.ENGLISH,
            lane=DescriptionExpansionRoute.ANALYTICAL,
        )
    )

    assert result.usage is None
    assert result.expansion.probes[0].semantic_focus == ("customer", "identifier")
    assert result.expansion.probes[0].owner_focus == ("customer",)
    assert delegate.calls == 0
    assert control.reservations == []
    assert control.settlements == []


def test_provider_delegate_cannot_report_local_none_usage_after_admission() -> None:
    configuration = _configuration()
    base = _Expansion(configuration)

    @dataclass
    class _MissingUsage:
        calls: int = 0

        @property
        def configuration(self) -> ProviderConfigurationFacts:
            return configuration

        def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
            self.calls += 1
            result = base.expand(value)
            return DescriptionExpansionResult(
                expansion=result.expansion,
                usage=None,
            )

    delegate = _MissingUsage()
    control = _Control(configuration)

    with pytest.raises(QueryStudioPortError) as captured:
        _expansion_wrapper(delegate, control).expand(_input())  # type: ignore[arg-type]

    assert captured.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert delegate.calls == 1
    assert len(control.reservations) == 1
    assert [item.outcome for item in control.settlements] == [AiSettlementOutcome.INVALID_OUTPUT]


@pytest.mark.parametrize(
    "usage_update",
    (
        {"model_snapshot": "gpt-5.4-nano-2026-03-17"},
        {"configuration_fingerprint": "f" * 64},
        {"outcome": ProviderOutcomeCode.TIMEOUT},
        {"input_tokens": 0, "output_tokens": 0},
        {"input_tokens": 0},
        {"output_tokens": 0},
        {"input_tokens": 48_001},
        {"output_tokens": 4_097},
    ),
)
def test_expansion_usage_must_match_the_exact_admitted_reservation(
    usage_update: dict[str, object],
) -> None:
    configuration = _configuration()
    delegate = _ExpansionWithUsage(
        delegate=_Expansion(configuration),
        usage=_usage(configuration, ProviderStage.EXPANSION).model_copy(update=usage_update),
    )
    control = _Control(configuration)

    with pytest.raises(QueryStudioPortError) as captured:
        _expansion_wrapper(delegate, control).expand(_input())  # type: ignore[arg-type]

    assert captured.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert delegate.calls == 1
    assert [item.outcome for item in control.settlements] == [AiSettlementOutcome.INVALID_OUTPUT]
    assert control.settlements[0].observed_input_tokens is None
    assert control.settlements[0].observed_output_tokens is None


def test_one_transient_retry_has_a_distinct_reservation_and_settlement() -> None:
    configuration = _configuration()
    delegate = _Expansion(
        configuration,
        failures=[
            QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_TIMEOUT,
                "sanitized timeout",
            )
        ],
    )
    control = _Control(
        configuration,
        outcomes=[AiAdmissionOutcome.RESERVED, AiAdmissionOutcome.RESERVED],
    )

    result = _expansion_wrapper(delegate, control).expand(_input())

    assert result.usage.outcome is ProviderOutcomeCode.SUCCEEDED
    assert delegate.calls == 2
    assert [item.attempt_number for item in control.reservations] == [1, 2]
    assert len({item.idempotency_digest for item in control.reservations}) == 2
    assert [item.outcome for item in control.settlements] == [
        AiSettlementOutcome.TIMEOUT,
        AiSettlementOutcome.SUCCEEDED,
    ]


def test_one_retryable_invalid_response_is_settled_before_exactly_one_retry() -> None:
    configuration = _configuration()
    delegate = _Expansion(
        configuration,
        failures=[
            QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "sanitized invalid structured output",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
            )
        ],
    )
    control = _Control(
        configuration,
        outcomes=[AiAdmissionOutcome.RESERVED, AiAdmissionOutcome.RESERVED],
    )

    result = _expansion_wrapper(delegate, control).expand(_input())

    assert result.usage.outcome is ProviderOutcomeCode.SUCCEEDED
    assert delegate.calls == 2
    assert [item.attempt_number for item in control.reservations] == [1, 2]
    assert len({item.idempotency_digest for item in control.reservations}) == 2
    assert [item.outcome for item in control.settlements] == [
        AiSettlementOutcome.INVALID_OUTPUT,
        AiSettlementOutcome.SUCCEEDED,
    ]


def test_two_retryable_invalid_responses_fail_without_a_third_attempt() -> None:
    configuration = _configuration()
    invalid = QueryStudioPortError(
        QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
        "sanitized invalid structured output",
        retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
    )
    delegate = _Expansion(configuration, failures=[invalid, invalid])
    control = _Control(
        configuration,
        outcomes=[AiAdmissionOutcome.RESERVED, AiAdmissionOutcome.RESERVED],
    )

    with pytest.raises(QueryStudioPortError) as captured:
        _expansion_wrapper(delegate, control).expand(_input())

    assert captured.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert delegate.calls == 2
    assert [item.attempt_number for item in control.reservations] == [1, 2]
    assert [item.outcome for item in control.settlements] == [
        AiSettlementOutcome.INVALID_OUTPUT,
        AiSettlementOutcome.INVALID_OUTPUT,
    ]


def test_deterministic_invalid_request_never_retries() -> None:
    configuration = _configuration()
    delegate = _Expansion(
        configuration,
        failures=[
            QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "sanitized deterministic invalid request",
            )
        ],
    )
    control = _Control(
        configuration,
        outcomes=[AiAdmissionOutcome.RESERVED, AiAdmissionOutcome.RESERVED],
    )

    with pytest.raises(QueryStudioPortError):
        _expansion_wrapper(delegate, control).expand(_input())

    assert delegate.calls == 1
    assert [item.attempt_number for item in control.reservations] == [1]
    assert [item.outcome for item in control.settlements] == [AiSettlementOutcome.INVALID_OUTPUT]


def test_retryable_invalid_response_can_be_denied_before_second_egress() -> None:
    configuration = _configuration()
    delegate = _Expansion(
        configuration,
        failures=[
            QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "sanitized invalid structured output",
                retry_disposition=QueryStudioRetryDisposition.RETRY_ONCE,
            )
        ],
    )
    control = _Control(
        configuration,
        outcomes=[AiAdmissionOutcome.RESERVED, AiAdmissionOutcome.QUOTA_EXHAUSTED],
    )

    with pytest.raises(QueryStudioPortError) as captured:
        _expansion_wrapper(delegate, control).expand(_input())

    assert captured.value.code is QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED
    assert delegate.calls == 1
    assert [item.attempt_number for item in control.reservations] == [1, 2]
    assert [item.outcome for item in control.settlements] == [AiSettlementOutcome.INVALID_OUTPUT]


def test_quota_denial_stops_before_provider_and_has_no_settlement() -> None:
    configuration = _configuration()
    delegate = _Expansion(configuration)
    control = _Control(
        configuration,
        outcomes=[AiAdmissionOutcome.QUOTA_EXHAUSTED],
    )

    with pytest.raises(QueryStudioPortError) as captured:
        _expansion_wrapper(delegate, control).expand(_input())

    assert captured.value.code is QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED
    assert delegate.calls == 0
    assert control.settlements == []


def test_intermediate_quota_uses_closed_input_bound_and_never_calls_delegate() -> None:
    configuration = _configuration()
    delegate = _Expansion(configuration)
    control = _Control(
        configuration,
        remaining_input_tokens=47_999,
    )

    with pytest.raises(QueryStudioPortError) as captured:
        _expansion_wrapper(delegate, control).expand(_input())

    assert captured.value.code is QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED
    assert control.reservations[0].estimated_input_tokens == 48_000
    assert delegate.calls == 0
    assert control.settlements == []


def test_reserved_configuration_mismatch_is_charged_and_fails_closed() -> None:
    configuration = _configuration()
    delegate = _Expansion(configuration)
    control = _Control(configuration)
    control.configuration = ProviderConfigurationFacts.create(
        **{
            **configuration.model_dump(mode="python", exclude={"fingerprint"}),
            "model_snapshot": "gpt-5.4-nano-2026-03-17",
        }
    )

    with pytest.raises(QueryStudioPortError) as captured:
        _expansion_wrapper(delegate, control).expand(_input())

    assert captured.value.code is QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE
    assert delegate.calls == 0
    assert control.settlements[0].outcome is AiSettlementOutcome.INVALID_OUTPUT


def test_typed_intent_uses_the_same_durable_attempt_boundary() -> None:
    configuration = _configuration()
    delegate = _Intent(configuration)
    control = _Control(configuration)
    wrapper = AdmittedQueryStudioIntent(
        delegate=delegate,
        control=control,
        nonces=_Nonces(),
        workspace_id="workspace-alpha",
        actor_digest=SHA_A,
        semantic_scope_fingerprint=SHA_B,
        configuration=configuration,
        estimated_input_tokens=64_000,
        estimated_output_tokens=4_096,
    )

    result = wrapper.interpret(
        QueryStudioInterpretationInput(
            text=DescriptionQuery("count customers"),
            language=UserLanguage.ENGLISH,
            vocabulary=_vocabulary(),
            expansion=_interpretation_expansion(),
        )
    )

    assert result.proposal.semantic_state is SemanticMatchState.ALIGNED
    assert control.reservations[0].stage is ProviderStage.INTERPRETATION
    assert control.reservations[0].semantic_payload_fingerprint == _vocabulary().fingerprint
    assert control.settlements[0].outcome is AiSettlementOutcome.SUCCEEDED


def test_typed_intent_admission_accepts_the_complete_4096_output_token_budget() -> None:
    configuration = _configuration()
    delegate = _IntentWithUsage(
        delegate=_Intent(configuration),
        usage=_usage(configuration, ProviderStage.INTERPRETATION).model_copy(
            update={"output_tokens": 4_096}
        ),
    )
    control = _Control(configuration)
    wrapper = AdmittedQueryStudioIntent(
        delegate=delegate,
        control=control,
        nonces=_Nonces(),
        workspace_id="workspace-alpha",
        actor_digest=SHA_A,
        semantic_scope_fingerprint=SHA_B,
        configuration=configuration,
        estimated_input_tokens=64_000,
        estimated_output_tokens=4_096,
    )

    result = wrapper.interpret(
        QueryStudioInterpretationInput(
            text=DescriptionQuery("count customers"),
            language=UserLanguage.ENGLISH,
            vocabulary=_vocabulary(),
            expansion=_interpretation_expansion(),
        )
    )

    assert result.usage.output_tokens == 4_096
    assert control.reservations[0].estimated_output_tokens == 4_096
    assert control.settlements[0].observed_output_tokens == 4_096
    assert control.settlements[0].outcome is AiSettlementOutcome.SUCCEEDED


@pytest.mark.parametrize(
    "usage_update",
    (
        {"stage": ProviderStage.EXPANSION},
        {"model_snapshot": "gpt-5.4-nano-2026-03-17"},
        {"configuration_fingerprint": "f" * 64},
        {"outcome": ProviderOutcomeCode.TIMEOUT},
        {"input_tokens": 0, "output_tokens": 0},
        {"input_tokens": 0},
        {"output_tokens": 0},
        {"input_tokens": 64_001},
        {"output_tokens": 4_097},
    ),
)
def test_typed_intent_usage_must_match_the_exact_admitted_reservation(
    usage_update: dict[str, object],
) -> None:
    configuration = _configuration()
    delegate = _IntentWithUsage(
        delegate=_Intent(configuration),
        usage=_usage(configuration, ProviderStage.INTERPRETATION).model_copy(update=usage_update),
    )
    control = _Control(configuration)
    wrapper = AdmittedQueryStudioIntent(
        delegate=delegate,
        control=control,
        nonces=_Nonces(),
        workspace_id="workspace-alpha",
        actor_digest=SHA_A,
        semantic_scope_fingerprint=SHA_B,
        configuration=configuration,
        estimated_input_tokens=64_000,
        estimated_output_tokens=4_096,
    )

    with pytest.raises(QueryStudioPortError) as captured:
        wrapper.interpret(
            QueryStudioInterpretationInput(
                text=DescriptionQuery("count customers"),
                language=UserLanguage.ENGLISH,
                vocabulary=_vocabulary(),
                expansion=_interpretation_expansion(),
            )
        )

    assert captured.value.code is QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT
    assert delegate.calls == 1
    assert [item.outcome for item in control.settlements] == [AiSettlementOutcome.INVALID_OUTPUT]
    assert control.settlements[0].observed_input_tokens is None
    assert control.settlements[0].observed_output_tokens is None
