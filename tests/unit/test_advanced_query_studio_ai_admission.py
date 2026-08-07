from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from schemabridge.adapters.query_studio.advanced_fake_language import (
    M32_REFERENCE_QUESTION_ES,
    DeterministicAdvancedLanguageAdapter,
)
from schemabridge.adapters.query_studio.advanced_semantic_index import (
    RegistryWideAdvancedSemanticIndex,
)
from schemabridge.application.natural_sql import build_advanced_semantic_context
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
    AdvancedQueryStudioPortErrorCode,
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
    AdmittedAdvancedInterpretation,
    AdmittedAdvancedMentionExtraction,
)
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.advanced_query_studio import (
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
    AdvancedNaturalLanguageInput,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    ProviderConfigurationFacts,
    ProviderOutcomeCode,
    ProviderStage,
    ProviderUsageFacts,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
RESERVATION_ID = "air_" + "c" * 64
AUDIT_ID = "aia_" + "d" * 64


@dataclass(slots=True)
class _Nonces:
    index: int = 0

    def new_nonce(self) -> str:
        self.index += 1
        return f"advanced_capability_{self.index:04d}_" + "x" * 24


@dataclass(slots=True)
class _Control:
    configuration: ProviderConfigurationFacts
    outcomes: list[AiAdmissionOutcome] = field(
        default_factory=lambda: [AiAdmissionOutcome.RESERVED]
    )
    reservations: list[AiAttemptReservationRequest] = field(default_factory=list)
    settlements: list[AiAttemptSettlementRequest] = field(default_factory=list)

    def reserve(self, request: AiAttemptReservationRequest) -> AiAttemptReservation:
        self.reservations.append(request)
        outcome = self.outcomes.pop(0)
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


@dataclass(slots=True)
class _MentionDelegate:
    result: AdvancedMentionExtractionResult
    failures: list[AdvancedQueryStudioPortError] = field(default_factory=list)
    calls: int = 0

    def extract(
        self,
        value: AdvancedMentionExtractionInput,
    ) -> AdvancedMentionExtractionResult:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.result.model_copy(update={"input": value})


@dataclass(slots=True)
class _InterpretationDelegate:
    result: AdvancedInterpretationResult
    calls: int = 0

    def interpret(
        self,
        value: AdvancedInterpretationInput,
    ) -> AdvancedInterpretationResult:
        self.calls += 1
        return self.result.model_copy(update={"input": value})


def _configuration() -> ProviderConfigurationFacts:
    return ProviderConfigurationFacts.create(
        adapter="openai_responses_advanced",
        model_snapshot="gpt-5-nano-2025-08-07",
        reasoning_effort="minimal",
        endpoint_region="eu",
        prompt_version="m32-openai-v1",
        schema_version="m32-advanced-v1",
        matcher_version="m32-registry-wide-v1",
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
        input_tokens=80,
        output_tokens=20,
        duration_ms=17,
        outcome=ProviderOutcomeCode.SUCCEEDED,
    )


def _inputs(
    configuration: ProviderConfigurationFacts,
) -> tuple[
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
]:
    query = AdvancedNaturalLanguageInput(
        text=M32_REFERENCE_QUESTION_ES,
        language=UserLanguage.SPANISH,
    )
    mention_input = AdvancedMentionExtractionInput(query=query)
    language = DeterministicAdvancedLanguageAdapter()
    mention_result = language.extract(mention_input).model_copy(
        update={"usage": _usage(configuration, ProviderStage.EXPANSION)}
    )
    loaded = build_semantic_registry(repository_root=ROOT).load()
    retrieval = RegistryWideAdvancedSemanticIndex().retrieve(
        query=query,
        extraction=mention_result.extraction,
        registry=loaded.registry,
    )
    interpretation_input = AdvancedInterpretationInput(
        query=query,
        extraction=mention_result.extraction,
        context=build_advanced_semantic_context(
            loaded,
            retrieval.logical_fields,
        ),
    )
    interpretation_result = language.interpret(interpretation_input).model_copy(
        update={"usage": _usage(configuration, ProviderStage.INTERPRETATION)}
    )
    return (
        mention_input,
        mention_result,
        interpretation_input,
        interpretation_result,
    )


def test_advanced_mention_call_is_reserved_and_settled_without_raw_text() -> None:
    configuration = _configuration()
    mention_input, mention_result, _interpretation_input, _interpretation_result = _inputs(
        configuration
    )
    delegate = _MentionDelegate(mention_result)
    control = _Control(configuration)
    wrapper = AdmittedAdvancedMentionExtraction(
        delegate=delegate,
        control=control,
        nonces=_Nonces(),
        workspace_id="workspace-alpha",
        actor_digest="a" * 64,
        semantic_scope_fingerprint="b" * 64,
        configuration=configuration,
        estimated_input_tokens=64_000,
        estimated_output_tokens=4_096,
    )

    result = wrapper.extract(mention_input)

    assert result.extraction == mention_result.extraction
    assert delegate.calls == 1
    assert control.reservations[0].stage is ProviderStage.EXPANSION
    assert control.reservations[0].semantic_payload_fingerprint == (mention_input.query.digest)
    assert control.settlements[0].outcome is AiSettlementOutcome.SUCCEEDED
    assert control.settlements[0].observed_input_tokens == 80
    assert M32_REFERENCE_QUESTION_ES not in repr(control.reservations[0])


def test_transient_advanced_failure_uses_exactly_one_new_admitted_attempt() -> None:
    configuration = _configuration()
    mention_input, mention_result, _interpretation_input, _interpretation_result = _inputs(
        configuration
    )
    delegate = _MentionDelegate(
        mention_result,
        failures=[
            AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.PROVIDER_TIMEOUT,
                "sanitized provider timeout",
            )
        ],
    )
    control = _Control(
        configuration,
        outcomes=[AiAdmissionOutcome.RESERVED, AiAdmissionOutcome.RESERVED],
    )
    wrapper = AdmittedAdvancedMentionExtraction(
        delegate=delegate,
        control=control,
        nonces=_Nonces(),
        workspace_id="workspace-alpha",
        actor_digest="a" * 64,
        semantic_scope_fingerprint="b" * 64,
        configuration=configuration,
        estimated_input_tokens=64_000,
        estimated_output_tokens=4_096,
    )

    wrapper.extract(mention_input)

    assert delegate.calls == 2
    assert [item.attempt_number for item in control.reservations] == [1, 2]
    assert [item.outcome for item in control.settlements] == [
        AiSettlementOutcome.TIMEOUT,
        AiSettlementOutcome.SUCCEEDED,
    ]


def test_advanced_quota_denial_prevents_provider_egress() -> None:
    configuration = _configuration()
    mention_input, mention_result, _interpretation_input, _interpretation_result = _inputs(
        configuration
    )
    delegate = _MentionDelegate(mention_result)
    control = _Control(
        configuration,
        outcomes=[AiAdmissionOutcome.QUOTA_EXHAUSTED],
    )
    wrapper = AdmittedAdvancedMentionExtraction(
        delegate=delegate,
        control=control,
        nonces=_Nonces(),
        workspace_id="workspace-alpha",
        actor_digest="a" * 64,
        semantic_scope_fingerprint="b" * 64,
        configuration=configuration,
        estimated_input_tokens=64_000,
        estimated_output_tokens=4_096,
    )

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        wrapper.extract(mention_input)

    assert captured.value.code is (AdvancedQueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED)
    assert delegate.calls == 0
    assert control.settlements == []


def test_advanced_interpretation_binds_admission_to_exact_context() -> None:
    configuration = _configuration()
    _mention_input, _mention_result, interpretation_input, interpretation_result = _inputs(
        configuration
    )
    delegate = _InterpretationDelegate(interpretation_result)
    control = _Control(configuration)
    wrapper = AdmittedAdvancedInterpretation(
        delegate=delegate,
        control=control,
        nonces=_Nonces(),
        workspace_id="workspace-alpha",
        actor_digest="a" * 64,
        semantic_scope_fingerprint="b" * 64,
        configuration=configuration,
        estimated_input_tokens=96_000,
        estimated_output_tokens=4_096,
    )

    result = wrapper.interpret(interpretation_input)

    assert result.envelope == interpretation_result.envelope
    assert delegate.calls == 1
    assert control.reservations[0].stage is ProviderStage.INTERPRETATION
    assert control.reservations[0].semantic_payload_fingerprint == (
        interpretation_input.context.fingerprint
    )
    assert control.settlements[0].outcome is AiSettlementOutcome.SUCCEEDED
